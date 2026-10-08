"""Owner-issued management scope sharing the preparation admission transaction.

These records contain metadata and token hashes. They never select an executor,
release policy, provider, credential or artifact acquisition authority. Old
schema-1 jobs remain legacy owner jobs; every newly created schema-2 job records
its authority kind, so deleting a grant association cannot turn it into an
owner job. All synchronous checks use the import store's lock and connection.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import secrets
import time
import uuid

from .grants import GrantError, TOKEN, MAX_GRANTS
from .private import check_private_file, ensure_private_directory, write_private_json
from .preparation_contracts import PreparationError, canonical_bytes, contract_digest

MODES = {"preparation-execution", "resolution-execution", "recipe-management"}
RECIPE_ACTIONS = {"register", "enable", "disable", "deprecate", "revoke", "remove"}
OWNER_AUTHORITY = {"schema_version": 1, "kind": "owner"}


def _uuid(value):
    try:
        if str(uuid.UUID(value)) != value:
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise GrantError("invalid_grant", 400) from None


def _digest(value):
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise GrantError("invalid_grant", 400)


def _plan_identity(plan):
    # Preparation consumes only this mutable pointer; resolution stores no
    # pointer in its plan. Every other captured plan byte remains authoritative.
    return {key: value for key, value in plan.items() if key != "job_id"}


def job_identity(kind, job):
    if kind == "preparation":
        keys = ("job_id", "binding", "authority_plan_id", "authority_plan_digest", "runtime_digest",
                "bound_inputs_digest", "candidate_contract_digest", "fixture_digest")
    else:
        keys = ("job_id", "plan_id", "plan_digest", "request", "candidate")
    return contract_digest("management-workflow-job-v1", {"kind": kind,
        "schema_version": job.get("schema_version"), "management_authority": job.get("management_authority"),
        **{key: job.get(key) for key in keys}})


def init_job_authority(store):
    store.db.execute("""CREATE TABLE IF NOT EXISTS workflow_management_job_kinds (
        kind TEXT NOT NULL, job_id TEXT NOT NULL, authority TEXT NOT NULL, PRIMARY KEY(kind,job_id))""")


def record_job_authority(store, kind, job):
    if not store.db.in_transaction or job.get("schema_version") != 2:
        raise PreparationError("management_execution_authority_invalid")
    store.db.execute("INSERT INTO workflow_management_job_kinds VALUES (?,?,?)", (
        kind, job["job_id"], canonical_bytes(job["management_authority"]).decode()))


def check_job_authority(store, kind, job):
    """Current checks must never reinterpret a new malformed record as owner."""
    version = job.get("schema_version")
    authority = job.get("management_authority")
    row = store.db.execute("SELECT authority FROM workflow_management_job_kinds WHERE kind=? AND job_id=?",
                           (kind, job["job_id"])).fetchone()
    if type(version) is int and version == 1 and authority is None and row is None:
        return  # Immutable pre-upgrade owner job; never rewrite it on inspection.
    if (type(version) is not int or version != 2 or not isinstance(authority, dict) or row is None
            or row[0] != canonical_bytes(authority).decode()):
        raise PreparationError("management_execution_authority_invalid")
    if authority == OWNER_AUTHORITY and type(authority.get("schema_version")) is int:
        return
    ledger = getattr(store, "management_execution_grants", None)
    if ledger is None:
        raise PreparationError("management_execution_authority_invalid")
    try:
        ledger.check_job(kind, job)
    except GrantError as error:
        code = {"grant_revoked": "management_execution_revoked", "grant_expired": "management_execution_expired"}.get(
            error.code, "management_execution_authority_invalid")
        raise PreparationError(code) from None


def start_authority(store, kind, plan, key, grant_id):
    ledger = getattr(store, "management_execution_grants", None)
    if ledger is None:
        if grant_id is not None:
            raise GrantError("grants_unavailable", 503)
        return dict(OWNER_AUTHORITY)
    return ledger.start_authority(kind, plan, key, grant_id)


def existing_start(store, kind, job, authority):
    ledger = getattr(store, "management_execution_grants", None)
    if ledger is None:
        if authority != OWNER_AUTHORITY:
            raise GrantError("grants_unavailable", 503)
        check_job_authority(store, kind, job)
        return
    ledger.existing_start(kind, job, authority)


class WorkflowGrantStore:
    def __init__(self, preparations, resolutions, recipes):
        self.preparations, self.resolutions, self.recipes = preparations, resolutions, recipes
        self.imports = preparations.imports
        if resolutions.imports is not self.imports or recipes.imports is not self.imports:
            raise GrantError("grants_unavailable", 503)
        self.db = self.imports._connection
        self.root = ensure_private_directory(self.imports.root / "preparation-grants")
        with self.imports._transaction():
            self.db.execute("""CREATE TABLE IF NOT EXISTS workflow_management_grants (
                grant_id TEXT PRIMARY KEY, token_hash TEXT UNIQUE NOT NULL, mode TEXT NOT NULL,
                created_at REAL NOT NULL, expires_at REAL NOT NULL, revoked INTEGER NOT NULL,
                scope_digest TEXT NOT NULL, scope TEXT NOT NULL, job_id TEXT)""")
            self.db.execute("""CREATE TABLE IF NOT EXISTS workflow_management_jobs (
                kind TEXT NOT NULL, job_id TEXT NOT NULL, grant_id TEXT UNIQUE NOT NULL,
                scope_digest TEXT NOT NULL, binding_digest TEXT NOT NULL, PRIMARY KEY(kind,job_id))""")
        preparations.management_execution_grants = self
        resolutions.management_execution_grants = self
        # Only files named by this ledger can be reclaimed. Missing rows leave
        # copied credentials powerless; unexpected entries are never removed.
        with self.imports._lock:
            for path in self.root.iterdir():
                try:
                    _uuid(path.stem)
                except GrantError:
                    continue
                if path.suffix != ".json":
                    continue
                row = self.db.execute("SELECT revoked FROM workflow_management_grants WHERE grant_id=?", (path.stem,)).fetchone()
                if row is None or row[0]:
                    self._reclaim(path.stem)

    def contains(self, grant_id):
        with self.imports._lock:
            return self.db.execute("SELECT 1 FROM workflow_management_grants WHERE grant_id=?", (grant_id,)).fetchone() is not None

    def _row(self, grant_id):
        _uuid(grant_id)
        row = self.db.execute("SELECT * FROM workflow_management_grants WHERE grant_id=?", (grant_id,)).fetchone()
        if row is None:
            raise GrantError("grant_not_found", 404)
        return row

    def _scope(self, row):
        try:
            scope = json.loads(row["scope"])
            metadata = {"schema_version", "mode", "grant_id", "created_at", "expires_at", "token_digest"}
            if "native_session" in scope:
                if scope["native_session"] is not True:
                    raise ValueError()
                metadata.add("native_session")
            fields = {"recipe", "actions", "path"} if row["mode"] == "recipe-management" else {"kind", "plan", "idempotency_key"}
            if (row["mode"] not in MODES or type(scope) is not dict or set(scope) != metadata | fields
                    or type(scope.get("schema_version")) is not int or scope["schema_version"] != 1
                    or scope.get("mode") != row["mode"]
                    or any(scope.get(key) != row[key] for key in ("grant_id", "created_at", "expires_at"))
                    or scope.get("token_digest") != row["token_hash"]
                    or contract_digest("management-workflow-scope-v1", scope) != row["scope_digest"]):
                raise ValueError()
            if row["mode"] != "recipe-management" and scope.get("kind") != row["mode"].removesuffix("-execution"):
                raise ValueError()
            return scope
        except (ValueError, TypeError, KeyError):
            raise GrantError("grant_scope_changed", 409) from None

    def _active(self, grant_id):
        row = self._row(grant_id)
        if row["revoked"]:
            raise GrantError("grant_revoked", 401)
        if time.time() >= row["expires_at"]:
            raise GrantError("grant_expired", 401)
        scope = self._scope(row)
        native_session = scope.get("native_session") is True
        guard = getattr(self, "session_guard", None)
        if native_session and guard is None:
            raise GrantError("grant_revoked", 401)
        if guard is not None:
            guard(grant_id, required=native_session)
        return row

    def _plan(self, kind, plan_id):
        _uuid(plan_id)
        if kind == "preparation":
            return self.preparations._execution_plan(plan_id)
        row = self.db.execute("SELECT data FROM resolution_plans WHERE plan_id=?", (plan_id,)).fetchone()
        if row is None:
            raise GrantError("grant_plan_mismatch", 409)
        return json.loads(row[0])

    def _check_plan(self, scope):
        plan = self._plan(scope["kind"], scope["plan"]["plan_id"])
        if _plan_identity(plan) != scope["plan"]:
            raise GrantError("grant_plan_changed", 409)
        return plan

    def issue(self, payload, *, export_credential=True):
        if not isinstance(payload, dict) or payload.get("mode") not in MODES:
            raise GrantError("invalid_grant", 400)
        mode = payload["mode"]
        ttl = payload.get("ttl_seconds", 300)
        if type(ttl) is not int or not 1 <= ttl <= 3600:
            raise GrantError("invalid_grant", 400)
        scope = {"schema_version": 1, "mode": mode}
        if mode == "recipe-management":
            allowed = {"mode", "ttl_seconds", "recipe_id", "version", "digest", "actions", "path"}
            if set(payload) - allowed or not {"recipe_id", "version", "digest", "actions"} <= set(payload):
                raise GrantError("invalid_grant", 400)
            actions = payload["actions"]
            if (not isinstance(actions, list) or not actions or not all(isinstance(a, str) for a in actions)
                    or len(actions) != len(set(actions)) or set(actions) - RECIPE_ACTIONS):
                raise GrantError("invalid_grant", 400)
            _digest(payload["digest"])
            if "register" in actions:
                from .recipes import validate_bundle
                path = payload.get("path")
                if not isinstance(path, str) or not Path(path).is_absolute():
                    raise GrantError("invalid_grant", 400)
                bundle = validate_bundle(Path(path))
                reference = {"recipe_id": bundle["manifest"]["id"], "version": bundle["manifest"]["version"], "digest": bundle["digest"]}
            else:
                if "path" in payload:
                    raise GrantError("invalid_grant", 400)
                record = self.recipes.inspect(payload["recipe_id"], payload["version"])
                reference = {key: record[key] for key in ("recipe_id", "version", "digest")}
            if reference != {key: payload[key] for key in reference}:
                raise GrantError("grant_scope_changed", 409)
            scope.update(recipe=reference, actions=sorted(actions), path=payload.get("path"))
        else:
            if set(payload) - {"mode", "ttl_seconds", "plan_id", "plan_digest", "idempotency_key"} or not {
                    "plan_id", "plan_digest", "idempotency_key"} <= set(payload):
                raise GrantError("invalid_grant", 400)
            _uuid(payload["idempotency_key"]);_digest(payload["plan_digest"])
            kind = mode.removesuffix("-execution")
            with self.imports._lock:
                plan = self._plan(kind, payload["plan_id"])
                if plan["plan_digest"] != payload["plan_digest"] or plan["expires_at"] <= time.time():
                    raise GrantError("grant_plan_mismatch", 409)
                if plan.get("job_id") is not None:
                    raise GrantError("grant_operation_mismatch", 409)
                scope.update(kind=kind, plan=_plan_identity(plan), idempotency_key=payload["idempotency_key"])
        grant_id, token = str(uuid.uuid4()), secrets.token_urlsafe(32)
        if not export_credential:
            scope["native_session"] = True
        path = self.root / (grant_id + ".json")
        retired = []
        try:
            with self.imports._transaction():
                if self.db.execute("SELECT count(*) FROM workflow_management_grants").fetchone()[0] >= MAX_GRANTS:
                    # Failed native handoffs are inactive history, not admitted
                    # attempts. Keep every job-bound record for its existing
                    # authority checks while reclaiming unused stale hashes.
                    retired = [row[0] for row in self.db.execute("""SELECT grant_id FROM workflow_management_grants
                        WHERE job_id IS NULL AND (revoked=1 OR expires_at<=?)""", (time.time(),))]
                    self.db.executemany("DELETE FROM workflow_management_grants WHERE grant_id=?", [(item,) for item in retired])
                    if self.db.execute("SELECT count(*) FROM workflow_management_grants").fetchone()[0] >= MAX_GRANTS:
                        raise GrantError("grant_limit", 409)
                if mode != "recipe-management":
                    self._check_plan(scope)
                if export_credential:
                    write_private_json(path, {"schema_version": 1, "grant_id": grant_id, "token": token})
                now = time.time()
                scope.update(grant_id=grant_id, created_at=now, expires_at=now + ttl,
                             token_digest=hashlib.sha256(token.encode()).hexdigest())
                self.db.execute("INSERT INTO workflow_management_grants VALUES (?,?,?,?,?,?,?,?,?)", (
                    grant_id, scope["token_digest"], mode, now, now + ttl, 0,
                    contract_digest("management-workflow-scope-v1", scope), canonical_bytes(scope).decode(), None))
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        for retired_id in retired:
            self._reclaim(retired_id)
        if not export_credential:
            return {"grant_id": grant_id, "token": token, "expires_at": now + ttl}
        return self.inspect(grant_id)

    def inspect(self, grant_id):
        with self.imports._lock:
            row = self._row(grant_id);scope = self._scope(row)
            result = {key: row[key] for key in ("grant_id", "mode", "created_at", "expires_at", "scope_digest", "job_id")}
            result.update(revoked=bool(row["revoked"]), expired=time.time() >= row["expires_at"])
            if scope.get("native_session") is not True:
                result["credential_file"] = str(self.root / (grant_id + ".json"))
            if row["mode"] == "recipe-management":
                result.update(recipe=scope["recipe"], actions=scope["actions"])
            else:
                plan = scope["plan"]
                result.update(plan_id=plan["plan_id"], plan_digest=plan["plan_digest"], idempotency_key=scope["idempotency_key"],
                              import_id=plan.get("import_id", plan.get("request", {}).get("import_id")),
                              revision=plan.get("revision", plan.get("request", {}).get("revision")))
            return result

    def list(self):
        with self.imports._lock:
            rows = self.db.execute("SELECT grant_id FROM workflow_management_grants ORDER BY created_at DESC LIMIT 100").fetchall()
            return {"grants": [self.inspect(row[0]) for row in rows],
                    "total": self.db.execute("SELECT count(*) FROM workflow_management_grants").fetchone()[0]}

    def authenticate(self, token):
        if not isinstance(token, str) or not TOKEN.fullmatch(token):
            raise GrantError("unauthorized", 401)
        with self.imports._lock:
            row = self.db.execute("SELECT grant_id FROM workflow_management_grants WHERE token_hash=?", (
                hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
            if row is None:
                raise GrantError("unauthorized", 401)
            self._active(row[0])
            return {"grant_id": row[0], "workflow_grant": True}

    def _reclaim(self, grant_id):
        path = self.root / (grant_id + ".json")
        try:
            check_private_file(path);path.unlink()
        except Exception:
            pass

    def revoke(self, grant_id):
        with self.imports._transaction():
            self._row(grant_id)
            self.db.execute("UPDATE workflow_management_grants SET revoked=1 WHERE grant_id=?", (grant_id,))
        self._reclaim(grant_id)
        return self.inspect(grant_id)

    def start_authority(self, kind, plan, key, grant_id):
        if grant_id is None:
            return dict(OWNER_AUTHORITY)
        row = self._active(grant_id);scope = self._scope(row)
        if (scope.get("kind") != kind or row["mode"] != kind + "-execution" or scope.get("idempotency_key") != key
                or scope.get("plan") != _plan_identity(plan)):
            raise GrantError("grant_scope", 403)
        self._check_plan(scope)
        return {"schema_version": 1, "kind": "execution_grant", "grant_id": grant_id, "scope_digest": row["scope_digest"]}

    def existing_start(self, kind, job, authority):
        check_job_authority(self.preparations if kind == "preparation" else self.resolutions, kind, job)
        actual = job.get("management_authority")
        if authority == OWNER_AUTHORITY:
            if actual != OWNER_AUTHORITY and not (job.get("schema_version") == 1 and actual is None):
                raise GrantError("grant_operation_mismatch", 409)
            return
        if actual != authority:
            raise GrantError("grant_operation_mismatch", 409)
        self.check_job(kind, job)

    def bind_job(self, kind, job):
        if not self.db.in_transaction:
            raise GrantError("grants_unavailable", 503)
        authority = job["management_authority"]
        if authority == OWNER_AUTHORITY:
            return
        row = self._active(authority["grant_id"]);scope = self._scope(row)
        if row["job_id"] is not None or authority["scope_digest"] != row["scope_digest"] or scope.get("kind") != kind:
            raise GrantError("grant_operation_mismatch", 409)
        self._check_plan(scope)
        self.db.execute("INSERT INTO workflow_management_jobs VALUES (?,?,?,?,?)", (
            kind, job["job_id"], row["grant_id"], row["scope_digest"], job_identity(kind, job)))
        self.db.execute("UPDATE workflow_management_grants SET job_id=? WHERE grant_id=?", (job["job_id"], row["grant_id"]))

    def check_job(self, kind, job):
        with self.imports._lock:
            authority = job.get("management_authority")
            if (type(authority) is not dict or set(authority) != {"schema_version", "kind", "grant_id", "scope_digest"}
                    or type(authority["schema_version"]) is not int or authority["schema_version"] != 1
                    or authority["kind"] != "execution_grant"):
                raise GrantError("grant_scope_changed", 409)
            row = self._active(authority["grant_id"]);scope = self._scope(row)
            self._check_association(row, scope, kind, job)
            self._check_plan(scope)

    def _check_association(self, row, scope, kind, job):
        authority = job.get("management_authority")
        marker = self.db.execute("SELECT authority FROM workflow_management_job_kinds WHERE kind=? AND job_id=?",
                                 (kind, job["job_id"])).fetchone()
        associated = self.db.execute("SELECT * FROM workflow_management_jobs WHERE kind=? AND job_id=?",
                                     (kind, job["job_id"])).fetchone()
        expected = {"schema_version": 1, "kind": "execution_grant", "grant_id": row["grant_id"],
                    "scope_digest": row["scope_digest"]}
        if (type(job.get("schema_version")) is not int or job["schema_version"] != 2 or authority != expected
                or marker is None or marker[0] != canonical_bytes(expected).decode()
                or associated is None or row["job_id"] != job["job_id"] or scope.get("kind") != kind
                or associated["grant_id"] != row["grant_id"] or associated["scope_digest"] != row["scope_digest"]
                or associated["binding_digest"] != job_identity(kind, job)):
            raise GrantError("grant_operation_mismatch", 409)

    def cleanup_job(self, grant_id):
        """Revocation may drain only its original associated job, even after expiry.

        This does not admit execution or recapture a changed plan. Original job
        identity is enough to stop known owned work after its authority expires.
        Missing/corrupt associations deny cancellation of an unrelated job.
        """
        with self.imports._lock:
            row = self._row(grant_id);scope = self._scope(row)
            if row["job_id"] is None:
                return None
            kind = scope.get("kind")
            if kind not in {"preparation", "resolution"}:
                raise GrantError("grant_operation_mismatch", 409)
            store = self.preparations if kind == "preparation" else self.resolutions
            job = store._get(row["job_id"])
            self._check_association(row, scope, kind, job)
            return kind, job["job_id"]

    def authorize_route(self, grant_id, method, path, payload=None):
        with self.imports._lock:
            row = self._active(grant_id);scope = self._scope(row)
            if method == "GET" and path in {"/v1/capabilities", "/v1/recipes", "/v1/recipes/schema"}:
                return
            if row["mode"] == "recipe-management":
                if method == "POST" and path == "/v1/recipes" and "register" in scope["actions"] and payload == {"path": scope["path"]}:
                    return
                action = path.removeprefix("/v1/recipes/")
                if (method == "POST" and action in {*scope["actions"], "inspect"}
                        and payload == {key: scope["recipe"][key] for key in ("recipe_id", "version")}):
                    return
            else:
                kind=scope["kind"];collection="/v1/"+kind+"s"
                plan = scope["plan"]
                draft_binding = plan if kind == "preparation" else plan["request"]
                if method == "GET" and path == "/v1/imports/" + draft_binding["import_id"]:
                    draft = self.imports._get(draft_binding["import_id"], draft_binding["revision"])
                    if draft["source"]["revision"] != draft_binding["source_revision"]:
                        raise GrantError("grant_scope_changed", 409)
                    return
                if method == "POST" and path == collection + "/start" and payload == {
                        "plan_id": scope["plan"]["plan_id"], "plan_digest": scope["plan"]["plan_digest"], "idempotency_key": scope["idempotency_key"]}:
                    self._check_plan(scope);return
                if method == "GET" and path == collection:
                    return  # Dispatch filters to this one association.
                if row["job_id"] is not None and path == collection + "/" + row["job_id"] and method == "GET":
                    store = self.preparations if kind == "preparation" else self.resolutions
                    check_job_authority(store, kind, store._get(row["job_id"]))
                    return
                if (row["job_id"] is not None and method == "POST" and not payload
                        and path == collection + "/" + row["job_id"] + "/cancel"):
                    store = self.preparations if kind == "preparation" else self.resolutions
                    check_job_authority(store, kind, store._get(row["job_id"]))
                    return
            raise GrantError("grant_scope", 403)

    def filter_jobs(self, grant_id, jobs):
        with self.imports._lock:
            row = self._active(grant_id)
            scope = self._scope(row)
            if row["job_id"] is not None:
                store = self.preparations if scope["kind"] == "preparation" else self.resolutions
                check_job_authority(store, scope["kind"], store._get(row["job_id"]))
            return [job for job in jobs if job["job_id"] == row["job_id"]]

    def recipe_admit(self, grant_id, action, record):
        row=self._active(grant_id);scope=self._scope(row)
        if (row["mode"] != "recipe-management" or action not in scope["actions"]
                or scope["recipe"] != {key: record[key] for key in ("recipe_id", "version", "digest")}):
            raise GrantError("grant_scope_changed", 409)
