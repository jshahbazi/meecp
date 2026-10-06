"""Owner-reviewed agent proposals bound to one draft, recipe and attempt.

Provider implementations and credential resolvers come from trusted host code.
Public defaults dispatch nothing. This journal contains metadata only; bounded
typed proposal artifacts remain untrusted in the separate attempt store.
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
import re
import threading
import time
from typing import Annotated, Literal
import uuid

from pydantic import Field, ValidationError, model_validator

from .contracts import StrictModel
from .preparation_agent_backend import AgentBackend, ENDPOINT
from .preparation_agent_contracts import AnalysisGuideV1, AnalysisRequestV1, AgentLimitsV1, SourceFileV1, SourceMetadataV1
from .preparation_agent_guides import AnalysisGuideRegistry, builtin_analysis_guides
from .preparation_agent_recipe_choices import (AgentRecipeChoiceRegistry, MAX_AGENT_RECIPE_CHOICES,
                                              builtin_recipe_choice_rules, source_choice_inventory)
from .preparation_agent_coordinator import AgentAttemptStore, AgentCoordinator, TERMINAL
from .preparation_application_source import _read_file, _scan, application_source_records
from .preparation_selection_snapshot import selection_snapshot_metadata
from .preparation_contracts import PreparationError, RecipeReference, canonical_bytes, contract_digest, load_contract

UUID = Annotated[str, Field(pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Reference = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")]
MAX_PLANS = 1024
HOST_TERMINAL = TERMINAL | {"adopted"}


class AgentPlanRequest(StrictModel):
    import_id: UUID
    revision: Annotated[int, Field(ge=1, le=2**31 - 1)]
    recipe: RecipeReference
    provider_id: Reference
    model: Reference
    selected_paths: Annotated[list[str], Field(min_length=1, max_length=32)]
    selected_metadata_paths: Annotated[list[str], Field(max_length=32)] = Field(default_factory=list)
    limits: AgentLimitsV1 = Field(default_factory=AgentLimitsV1)

    @model_validator(mode="after")
    def closed_selection(self):
        if (self.selected_paths != sorted(set(self.selected_paths))
                or self.selected_metadata_paths != sorted(set(self.selected_metadata_paths))
                or set(self.selected_paths) & set(self.selected_metadata_paths)):
            raise ValueError("agent_source_scope_invalid")
        return self


class AnalyzeStart(StrictModel):
    action: Literal["analyze"]
    plan_id: UUID
    plan_digest: Digest
    idempotency_key: UUID


class AdoptStart(StrictModel):
    action: Literal["adopt-candidate"]
    job_id: UUID
    proposal_digest: Digest


@dataclass(frozen=True)
class AgentProviderConfiguration:
    """Not deserialized from requests or source. No arbitrary destination field."""
    provider_id: str
    backend: AgentBackend
    models: tuple[str, ...]
    credential_ref: str
    simulated: bool = False
    generation: str | None = None
    destination: str = ENDPOINT
    adapter_digest: str | None = None
    name: str | None = None

    def __post_init__(self):
        pattern = r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}"
        if (not re.fullmatch(pattern, self.provider_id) or not re.fullmatch(pattern, self.credential_ref)
                or type(self.models) is not tuple or not 1 <= len(self.models) <= 16
                or len(set(self.models)) != len(self.models)
                or any(not isinstance(value, str) or not re.fullmatch(pattern, value) for value in self.models)
                or type(self.simulated) is not bool or not re.fullmatch(pattern, self.backend.backend_id)
                or self.generation is not None and not re.fullmatch(r"[0-9a-f-]{36}", self.generation)
                or self.adapter_digest is not None and not re.fullmatch(r"[0-9a-f]{64}", self.adapter_digest)
                or not isinstance(self.destination, str) or not 1 <= len(self.destination) <= 256
                or any(ord(char) < 32 for char in self.destination)):
            raise PreparationError("agent_provider_configuration_invalid")

    @property
    def digest(self):
        return contract_digest("agent-host-provider-v1", {"id": self.provider_id,
            "backend": self.backend.backend_id, "models": list(self.models),
            "credential_ref": self.credential_ref, "simulated": self.simulated, "destination": self.destination,
            "generation": self.generation, "adapter_digest": self.adapter_digest, "name": self.name})


async def _join(task):
    """Repeated caller cancellation cannot abandon an already owned task."""
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
        except Exception:
            break
    return task.result()


class _AdmittedBackend:
    """Recheck held artifact authority immediately before each transport call."""
    def __init__(self, backend, admission, authority):
        self.backend, self.admission, self.authority = backend, admission, authority
        self.backend_id = backend.backend_id

    def encode(self, *args):
        return self.backend.encode(*args)

    async def complete(self, *args):
        task = asyncio.create_task(asyncio.to_thread(lambda: self.admission.check_current(full=True)))
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            try:
                await _join(task)
            except Exception:
                pass
            raise
        self.authority(args[0])
        return await self.backend.complete(*args)


class AgentPreparationHost:
    def __init__(self, preparations, root: Path, *, providers=(), live_enabled=False, simulated_enabled=False,
                 guides=None, choice_rules=None, provider_registry=None, execution_admission=None):
        from .preparation_release_policy import AgentExecutionAdmission
        if execution_admission is not None and (type(execution_admission) is not AgentExecutionAdmission
                or live_enabled is not False or simulated_enabled is not False or providers):
            raise PreparationError("invalid_agent_execution_admission")
        if type(live_enabled) is not bool or type(simulated_enabled) is not bool:
            raise PreparationError("agent_host_configuration_invalid")
        self.preparations, self.imports, self.recipes = preparations, preparations.imports, preparations.recipes
        self.providers = {value.provider_id: value for value in providers}
        self.provider_registry = provider_registry
        if len(self.providers) != len(providers) or len(providers) > 16:
            raise PreparationError("agent_provider_configuration_invalid")
        self.live_enabled, self.simulated_enabled = live_enabled, simulated_enabled
        self.execution_admission = execution_admission
        self.guides = AnalysisGuideRegistry(builtin_analysis_guides() if guides is None else guides)
        self.recipe_choices = AgentRecipeChoiceRegistry(builtin_recipe_choice_rules() if choice_rules is None else choice_rules)
        self.store = AgentAttemptStore(Path(root))
        self.tasks, self.stops, self.children = {}, {}, {}
        self.static_workers = set()
        self.closing = False
        self._close_task = None
        try:
            self.db = self.store.db
            self.db.execute("CREATE TABLE IF NOT EXISTS agent_host_plans (id TEXT PRIMARY KEY, data TEXT NOT NULL)")
            self.db.execute("CREATE TABLE IF NOT EXISTS agent_host_jobs (id TEXT PRIMARY KEY, idem TEXT UNIQUE NOT NULL, data TEXT NOT NULL)")
            with self.store.transaction():
                for row in self.db.execute("SELECT data FROM agent_host_jobs").fetchall():
                    job = json.loads(row["data"])
                    if job["state"] == "adopting" and self._adoption_matches(job):
                        job.update(state="adopted", adopted_revision=job["revision"] + 1, local_io_settled=True)
                        self._save_job(job)
                        continue
                    if job["state"] not in HOST_TERMINAL:
                        job.update(state="interrupted", local_io_settled=True, error_code="agent_host_restarted")
                        self._save_job(job)
        except BaseException:
            self.store.close()
            raise

    def _available(self, provider, model=None):
        if self.execution_admission is not None:
            try:
                self.execution_admission.check_current()
            except PreparationError:
                return False
            return (not self.closing and not provider.simulated and any(
                self.execution_admission.permits(provider.backend.backend_id, provider.adapter_digest, value)
                for value in ((model,) if model is not None else provider.models)))
        return self.simulated_enabled if provider.simulated else self.live_enabled

    def _provider(self, provider_id, model):
        provider = self.providers.get(provider_id)
        if provider is None and self.provider_registry is not None:
            try:
                provider = self.provider_registry.configuration(provider_id)
            except Exception:
                raise PreparationError("agent_provider_unavailable") from None
        if provider is None or model not in provider.models:
            raise PreparationError("agent_provider_unavailable")
        return provider

    def _providers(self):
        values = list(self.providers.values())
        if self.provider_registry is not None:
            values.extend(self.provider_registry.configurations())
        if len({value.provider_id for value in values}) != len(values):
            raise PreparationError("agent_provider_configuration_invalid")
        return values

    def _draft(self, import_id, revision):
        with self.imports._lock:
            draft = self.imports._get(import_id, revision)
            self.imports._mutable(draft)
        if draft["source"]["kind"] not in {"local_folder", "application_directory"}:
            raise PreparationError("agent_directory_required")
        if draft["attachment"] is not None:
            raise PreparationError("agent_unprepared_directory_required")
        return draft

    def options(self, import_id, revision):
        """Static metadata inventory; this never reads credentials or dispatches."""
        draft = self._draft(import_id, revision)
        enabled = [value for value in self.recipes.list() if value["state"] == "enabled"]
        displayed = enabled[:MAX_AGENT_RECIPE_CHOICES]
        source_inventory = source_choice_inventory(draft["source"])
        choices = self.recipe_choices.choices(draft["source"], self.recipes, self.guides, displayed,
                                             source_inventory=source_inventory)
        if len(enabled) > len(displayed):
            for choice in choices:
                choice["recommended"] = False
        return {"schema_version": 1, "import_id": import_id, "revision": revision,
            "source_revision": draft["source"]["revision"],
            "providers": [{"provider_id": value.provider_id, "models": list(value.models),
                "name": value.name or value.provider_id, "generation": value.generation,
                "backend": value.backend.backend_id, "destination": value.destination, "simulated": value.simulated,
                "execution_available": self._available(value)} for value in self._providers()],
            **source_inventory,
            "recipes": [{"id": value["recipe_id"], "version": value["version"], "digest": value["digest"]}
                        for value in displayed],
            "recipes_truncated": len(enabled) > len(displayed),
            "recipe_choices": choices,
            "latest_job": next((self.get(json.loads(row[0])["job_id"]) for row in self.db.execute(
                "SELECT data FROM agent_host_jobs ORDER BY rowid DESC") if (json.loads(row[0])["import_id"],
                json.loads(row[0])["revision"], json.loads(row[0])["source_revision"]) ==
                (import_id, revision, draft["source"]["revision"])), None),
            "latest_setup_job": self.app_setups.latest(import_id) if hasattr(self, "app_setups") else None,
            "monetary_ceiling_enforced": False}

    def _request(self, plan, draft):
        provider = self._provider(plan["provider_id"], plan["model"])
        from .app_setup_contracts import RepairAnalysisRequestV1
        repair = plan.get("repair_context")
        contract = RepairAnalysisRequestV1 if repair is not None else AnalysisRequestV1
        return contract(import_id=plan["import_id"], revision=plan["revision"],
            source_revision=plan["source_revision"], authority_id=plan["plan_id"], expires_at=plan["expires_at"],
            provider={"backend": provider.backend.backend_id, "model": plan["model"], "credential_ref": provider.credential_ref,
                "provider_id": provider.provider_id, "configuration_generation": provider.generation,
                "adapter_digest": provider.adapter_digest, "destination": provider.destination},
            selected_files=plan["selected_files"], intent=draft["intent"],
            selected_metadata=plan.get("selected_metadata", []), recipe=plan["recipe"], guide=self._guide(plan),
            permitted_tools=["read-selected-source", "propose-files"], limits=plan["limits"],
            **({"repair_context": repair} if repair is not None else {}))

    def _validate(self, plan, *, dispatch=False):
        if self.execution_admission is not None:
            self.execution_admission.check_current()
        if self.closing:
            raise PreparationError("agent_host_closing")
        if time.time() >= plan["expires_at"]:
            raise PreparationError("agent_authority_expired")
        draft = self._draft(plan["import_id"], plan["revision"])
        if contract_digest("agent-host-source-v1", draft["source"]) != plan["source_identity_digest"]:
            raise PreparationError("agent_source_identity_mismatch")
        if self._snapshot_metadata(draft) != plan.get("snapshot_metadata_digest"):
            raise PreparationError("agent_source_identity_mismatch")
        provider = self._provider(plan["provider_id"], plan["model"])
        if provider.digest != plan["provider_digest"]:
            raise PreparationError("agent_provider_changed")
        if plan.get("execution_admission_digest") != (self.execution_admission.digest if self.execution_admission else None):
            raise PreparationError("agent_execution_admission_changed")
        if dispatch and not self._available(provider, plan["model"]):
            raise PreparationError("agent_execution_disabled")
        ref = plan["recipe"]
        recipe = self.recipes.assert_admissible(ref["id"], ref["version"], ref["digest"])
        if recipe["epoch"] != plan["recipe_epoch"]:
            raise PreparationError("agent_recipe_changed")
        self._guide(plan, recipe)
        if self.execution_admission is not None:
            guide = self.guides.resolve(recipe)
            component = AnalysisGuideV1.model_validate(guide).component if guide is not None else None
            if component is None or (component.id, component.version, component.digest) not in self.execution_admission.components:
                raise PreparationError("agent_recipe_not_admitted")
        request = self._request(plan, draft)
        if request.digest != plan["request_digest"]:
            raise PreparationError("agent_scope_changed")
        return request, draft, provider

    @staticmethod
    def _guide_binding(value):
        if value is None:
            return None
        guide = AnalysisGuideV1.model_validate(value)
        return {"id": guide.id, "digest": guide.digest, "component": guide.component.model_dump()}

    def _guide(self, plan, recipe=None):
        if recipe is None:
            ref = plan["recipe"]
            recipe = self.recipes.assert_admissible(ref["id"], ref["version"], ref["digest"])
        value = self.guides.resolve(recipe)
        if self._guide_binding(value) != plan.get("guide_binding"):
            raise PreparationError("agent_guide_changed")
        return value

    @staticmethod
    def _snapshot_metadata(draft):
        data = json.dumps(asdict(selection_snapshot_metadata(draft)), sort_keys=True,
            separators=(",", ":"), allow_nan=False).encode()
        return hashlib.sha256(b"agent-host-snapshot-metadata-v1\0" + data).hexdigest()

    @staticmethod
    def _selected_bytes(draft, records, check, content_paths=None):
        # Every reviewed identity receives the same whole-byte proof. Only the
        # explicitly selected content subset must additionally be text.
        content_paths = {item["path"] for item in records} if content_paths is None else set(content_paths)
        root = Path(draft["source"]["snapshot_path"])
        if draft["source"]["kind"] == "application_directory":
            application_source_records(draft)
        expected = sorted(item["path"] for item in draft["source"]["files"])
        if _scan(root, private=True) != expected:
            raise PreparationError("agent_source_identity_mismatch")
        data, identities = {}, {}
        for item in records:
            check()
            payload, _, kind, identity = _read_file(root / item["path"], snapshot=True, with_identity=True)
            if ((item["path"] in content_paths and kind != "text")
                    or len(payload) != item["bytes"] or hashlib.sha256(payload).hexdigest() != item["sha256"]):
                raise PreparationError("agent_source_identity_mismatch")
            data[item["path"]], identities[item["path"]] = payload, identity
        check()
        if _scan(root, private=True) != expected:
            raise PreparationError("agent_source_identity_mismatch")
        for item in records:
            check()
            payload, _, _, identity = _read_file(root / item["path"], snapshot=True, with_identity=True)
            if payload != data[item["path"]] or identity != identities[item["path"]]:
                raise PreparationError("agent_source_identity_mismatch")
        check()
        return data

    def _reviewed_source(self, *args):
        if self.execution_admission is not None:
            self.execution_admission.check_current(full=True)
        result = self._selected_bytes(*args)
        if self.execution_admission is not None:
            self.execution_admission.check_current(full=True)
        return result

    async def plan(self, payload, *, repair_context=None):
        if self.closing:
            raise PreparationError("agent_host_closing")
        if len(self.static_workers) >= 4:
            raise PreparationError("agent_pending_limit")
        try:
            selection = AgentPlanRequest.model_validate(payload)
            draft = self._draft(selection.import_id, selection.revision)
            provider = self._provider(selection.provider_id, selection.model)
            if not draft["intent"]:
                raise PreparationError("agent_intent_required")
            recipe_worker = asyncio.create_task(asyncio.to_thread(self.recipes.resolve,
                selection.recipe.id, selection.recipe.version, selection.recipe.digest))
            self.static_workers.add(recipe_worker)
            try:
                recipe = await asyncio.shield(recipe_worker)
            except asyncio.CancelledError:
                try:
                    await _join(recipe_worker)
                except Exception:
                    pass
                raise
            finally:
                self.static_workers.discard(recipe_worker)
            records = {item["path"]: item for item in draft["source"]["files"]}
            selected = [SourceFileV1(path=path, sha256=records[path]["sha256"], bytes=records[path]["size"]).model_dump()
                        for path in selection.selected_paths]
            metadata = [SourceMetadataV1(path=path, sha256=records[path]["sha256"], bytes=records[path]["size"]).model_dump()
                        for path in selection.selected_metadata_paths]
            plan = {"schema_version": 1, "plan_id": str(uuid.uuid4()), "import_id": selection.import_id,
                "revision": selection.revision, "source_revision": draft["source"]["revision"],
                "source_identity_digest": contract_digest("agent-host-source-v1", draft["source"]),
                "snapshot_metadata_digest": self._snapshot_metadata(draft),
                "recipe": selection.recipe.model_dump(), "recipe_epoch": recipe["epoch"],
                "provider_id": provider.provider_id, "provider_digest": provider.digest, "model": selection.model,
                "execution_admission_digest": self.execution_admission.digest if self.execution_admission else None,
                "selected_files": selected, "selected_metadata": metadata,
                "guide_binding": self._guide_binding(self.guides.resolve(recipe)),
                "limits": selection.limits.model_dump(),
                "expires_at": int(time.time()) + 300 + selection.limits.wall_seconds, "job_id": None}
            if repair_context is not None:
                from .app_setup_contracts import RepairContextV1, DIAGNOSTICS
                repair = RepairContextV1.model_validate(repair_context)
                if repair.diagnostic_code not in DIAGNOSTICS:
                    raise PreparationError("app_setup_repair_scope_invalid")
                self.store.repair_input(repair)
                plan["repair_context"] = repair.model_dump()
            request = self._request(plan, draft)
            plan["request_digest"] = request.digest
            plan["plan_digest"] = contract_digest("agent-host-plan-v1", {key: value for key, value in plan.items() if key != "job_id"})
        except (ValidationError, KeyError, TypeError):
            raise PreparationError("agent_plan_invalid") from None
        # Static review proves the selected captured bytes are text and exactly
        # bound. It retains no source bytes in a plan or journal.
        # A joined recipe worker is not authority to create another worker after
        # shutdown has begun. This fence and task registration have no await.
        self._validate(plan)
        worker = asyncio.create_task(asyncio.to_thread(self._reviewed_source, draft, selected + metadata,
            lambda: None, selection.selected_paths))
        self.static_workers.add(worker)
        try:
            captured = await asyncio.shield(worker)
            request.selected_source({item["path"]: captured[item["path"]] for item in selected})
        except asyncio.CancelledError:
            try:
                await _join(worker)
            except Exception:
                pass
            raise
        finally:
            self.static_workers.discard(worker)
        self._validate(plan)
        with self.store.transaction():
            if self.db.execute("SELECT count(*) FROM agent_host_plans").fetchone()[0] >= MAX_PLANS:
                raise PreparationError("agent_plan_limit")
            self.db.execute("INSERT INTO agent_host_plans VALUES (?,?)", (plan["plan_id"], canonical_bytes(plan).decode()))
        return self.review(plan)

    def review(self, plan):
        provider = self._provider(plan["provider_id"], plan["model"])
        guide = self._guide(plan)
        return {key: plan[key] for key in ("schema_version", "plan_id", "plan_digest", "import_id", "revision",
            "source_revision", "recipe", "provider_id", "model", "selected_files", "selected_metadata", "limits", "expires_at")} | {
            "destination": provider.destination, "simulated": provider.simulated, "execution_available": self._available(provider, plan["model"]),
            "provider_generation": provider.generation,
            "guide": guide, "guide_digest": plan["guide_binding"]["digest"] if guide else None,
            "maximum_reserved_output_tokens": plan["limits"]["max_calls"] * plan["limits"]["max_output_tokens"],
            "monetary_ceiling_enforced": False, "remote_usage_after_cancel": "unknown",
            "blockers": [] if self._available(provider, plan["model"]) else ["agent_execution_disabled"]}

    def _plan(self, plan_id):
        row = self.db.execute("SELECT data FROM agent_host_plans WHERE id=?", (plan_id,)).fetchone()
        if row is None:
            raise PreparationError("agent_plan_not_found")
        return json.loads(row["data"])

    def _job(self, job_id):
        row = self.db.execute("SELECT data FROM agent_host_jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise PreparationError("agent_job_not_found")
        return json.loads(row["data"])

    def _save_job(self, job):
        self.db.execute("UPDATE agent_host_jobs SET data=? WHERE id=?", (canonical_bytes(job).decode(), job["job_id"]))

    async def start(self, payload):
        try:
            start = AnalyzeStart.model_validate(payload)
        except ValidationError:
            raise PreparationError("agent_start_invalid") from None
        plan = self._plan(start.plan_id)
        if plan["plan_digest"] != start.plan_digest:
            raise PreparationError("agent_plan_digest_mismatch")
        old = self.db.execute("SELECT data FROM agent_host_jobs WHERE idem=?", (start.idempotency_key,)).fetchone()
        if old is not None:
            job = json.loads(old["data"])
            if job["plan_id"] != start.plan_id:
                raise PreparationError("agent_idempotency_conflict")
            return self.get(job["job_id"])
        self._validate(plan, dispatch=True)
        if plan["job_id"] is not None:
            raise PreparationError("agent_authority_consumed")
        if len(self.tasks) >= 4:
            raise PreparationError("agent_pending_limit")
        job = {"schema_version": 1, "job_id": str(uuid.uuid4()), "plan_id": plan["plan_id"],
            "import_id": plan["import_id"], "revision": plan["revision"], "source_revision": plan["source_revision"],
            "recipe": plan["recipe"], "state": "queued", "local_io_settled": False, "error_code": None,
            "attempt_id": None, "proposal_digest": None, "adopted_revision": None,
            "candidate_metadata": None, "candidate_blocker": None}
        with self.store.transaction():
            plan["job_id"] = job["job_id"]
            self.db.execute("UPDATE agent_host_plans SET data=? WHERE id=?", (canonical_bytes(plan).decode(), plan["plan_id"]))
            self.db.execute("INSERT INTO agent_host_jobs VALUES (?,?,?)", (job["job_id"], start.idempotency_key, canonical_bytes(job).decode()))
        self.stops[job["job_id"]] = threading.Event()
        task = asyncio.create_task(self._run(job["job_id"]))
        self.tasks[job["job_id"]] = task
        return self.get(job["job_id"])

    async def _run(self, job_id):
        job = self._job(job_id)
        plan = self._plan(job["plan_id"])
        stop = self.stops[job_id]
        def check():
            if stop.is_set():
                raise PreparationError("agent_cancel_requested")
        worker = child = None
        try:
            request, draft, provider = self._validate(plan, dispatch=True)
            job["state"] = "snapshotting"; self._save_job(job)
            worker = asyncio.create_task(asyncio.to_thread(self._reviewed_source, draft,
                plan["selected_files"] + plan.get("selected_metadata", []), check,
                [item["path"] for item in plan["selected_files"]]))
            captured = await asyncio.shield(worker)
            source = {item["path"]: captured[item["path"]] for item in plan["selected_files"]}
            check()
            self._validate(plan, dispatch=True)
            def authority(current):
                if current.digest != plan["request_digest"]:
                    raise PreparationError("agent_scope_changed")
                check()
                self._validate(plan, dispatch=True)
            backend = (_AdmittedBackend(provider.backend, self.execution_admission, authority)
                if self.execution_admission is not None else provider.backend)
            coordinator = AgentCoordinator(self.store, backend, authority)
            attempt = coordinator.create(request)
            job.update(state="analyzing", attempt_id=attempt["attempt_id"]); self._save_job(job)
            child = asyncio.create_task(coordinator.run(attempt["attempt_id"], request, source))
            self.children[job_id] = child
            result = await asyncio.shield(child)
            check()
            self._validate(plan, dispatch=True)
            job.update(state="validating", proposal_digest=result.digest); self._save_job(job)
            def validate_candidate():
                if self.execution_admission is not None:
                    self.execution_admission.check_current(full=True)
                check()
                descriptor, draft = self._candidate_descriptor(job, plan, result, check)
                self._candidate_binding(descriptor, draft)
                if self.execution_admission is not None:
                    self.execution_admission.check_current(full=True)
                check()
                return {"descriptor_digest": descriptor.digest, "recipe": descriptor.recipe.model_dump()}
            worker = asyncio.create_task(asyncio.to_thread(validate_candidate))
            try:
                job["candidate_metadata"] = await asyncio.shield(worker)
            except Exception as error:
                from .app_setup_contracts import safe_code
                job["candidate_blocker"] = safe_code(error, "agent_candidate_requires_review")
            check(); self._validate(plan, dispatch=True)
            job.update(state="succeeded")
        except asyncio.CancelledError:
            stop.set()
            job.update(state="canceled", error_code=None)
        except Exception as error:
            from .app_setup_contracts import safe_code
            job.update(state="canceled" if stop.is_set() else "failed", error_code=None if stop.is_set() else safe_code(error, "agent_job_failed"))
        finally:
            if child is not None and not child.done():
                child.cancel()
            for owned in (worker, child):
                if owned is not None:
                    try:
                        await _join(owned)
                    except BaseException:
                        pass
            if job["state"] != "succeeded":
                self._cancel_generated_candidate(job)
            if job["attempt_id"] is not None:
                attempt = self.store.get(job["attempt_id"])
                if attempt["state"] not in TERMINAL:
                    self.store.finish(attempt["attempt_id"], attempt["generation"],
                        "canceled" if job["state"] == "canceled" else "failed", job["error_code"])
            job["local_io_settled"] = True
            self._save_job(job)
            self.tasks.pop(job_id, None); self.stops.pop(job_id, None); self.children.pop(job_id, None)

    def _cancel_generated_candidate(self, job):
        for family in self.preparations.families.values():
            if hasattr(family, "cancel_agent_proposal"):
                family.cancel_agent_proposal(job)

    def _candidate(self, job):
        plan = self._plan(job["plan_id"])
        request, _, _ = self._validate(plan)
        result = self.store.proposal(job["attempt_id"], request, lambda _: self._validate(plan))
        return self._candidate_descriptor(job, plan, result)

    def _candidate_descriptor(self, job, plan, result, check=lambda: None):
        if result.digest != job["proposal_digest"] or any(task.unresolved for task in result.tasks):
            raise PreparationError("agent_candidate_unresolved")
        # Only the exact registered trusted family selected by the reviewed
        # recipe may interpret generated source. Other families retain their
        # existing JSON-only blocker. A proposal cannot register a component.
        ref = plan["recipe"]
        record = self.recipes.resolve(ref["id"], ref["version"], ref["digest"])
        keys = {(item["component"], item["version"], item["digest"]) for item in record["manifest"]["steps"]}
        matching = [family for family in self.preparations.families.values() if keys == {
            item.reference.key for item in getattr(family, "components", (family.component,))}]
        if len(matching) == 1 and hasattr(matching[0], "freeze_agent_proposal"):
            observed = self._draft(job["import_id"], job["revision"])
            descriptor = matching[0].freeze_agent_proposal(job, plan, result, observed, check)
            descriptor, observed, _ = self.preparations._bound_descriptor(job["import_id"], job["revision"],
                descriptor.model_dump(mode="json", exclude_none=True), enforce_selection=False)
            if descriptor.recipe.model_dump() != plan["recipe"]:
                raise PreparationError("agent_candidate_recipe_mismatch")
            return descriptor, observed
        if any(not file.path.endswith(".json") for task in result.tasks for file in task.files):
            raise PreparationError("agent_generated_source_requires_review")
        files = result.tasks[-1].files
        if len(files) != 1 or files[0].path != "proposed/adapter/preparation.json":
            raise PreparationError("agent_generated_source_requires_review")
        descriptor, draft, _ = self.preparations._bound_descriptor(job["import_id"], job["revision"],
            load_contract(files[0].content.encode()), enforce_selection=False)
        if descriptor.recipe.model_dump() != plan["recipe"]:
            raise PreparationError("agent_candidate_recipe_mismatch")
        return descriptor, draft

    def _candidate_binding(self, descriptor, draft):
        if descriptor.schema_version == 2:
            family = self.preparations.family_for(descriptor)
            if hasattr(family, "selection_binding"):
                return family.selection_binding(descriptor, draft)
            if hasattr(family, "resolution_binding"):
                binding = family.resolution_binding(descriptor)
                if binding is not None:
                    return binding
        from .preparation_inspection import inspect_source
        candidates = inspect_source(draft)["candidates"]
        interface = descriptor.interface
        matched = [item for item in candidates if item["kind"] == interface.kind
            and item.get("module") == interface.module and item.get("function") == getattr(interface, "function", None)]
        if len(matched) != 1:
            raise PreparationError("agent_generated_form_unavailable")
        return matched[0]["candidate_id"], None

    def get(self, job_id):
        job = self._job(job_id)
        calls = []
        if job["attempt_id"] is not None:
            calls = self.store.get(job["attempt_id"])["calls"]
        candidate, blocker = None, None
        if job["state"] == "succeeded":
            try:
                self._validate(self._plan(job["plan_id"]))
                candidate = job.get("candidate_metadata")
                blocker = job.get("candidate_blocker")
            except Exception:
                blocker = "agent_candidate_requires_review"
        return {key: value for key, value in job.items() if key not in {"candidate_metadata", "candidate_blocker"}} | {
                "calls": calls, "candidate": candidate, "blockers": [blocker] if blocker else [],
                "remote_usage_after_cancel": "unknown", "verification": None, "installed": False}

    async def cancel(self, job_id):
        job = self._job(job_id)
        if job["state"] in HOST_TERMINAL and job_id in self.tasks:
            try:
                await _join(self.tasks[job_id])
            except BaseException:
                pass
            return self.get(job_id)
        if job["state"] not in HOST_TERMINAL:
            job["state"] = "cancel_requested"; self._save_job(job)
            stop = self.stops.get(job_id)
            if stop is not None:
                stop.set()
            task = self.tasks.get(job_id)
            if task is not None:
                task.cancel()
                try:
                    await _join(task)
                except BaseException:
                    pass
            # A task canceled before its first step still needs a durable row.
            job = self._job(job_id)
            if job["state"] == "cancel_requested":
                job.update(state="canceled", local_io_settled=True); self._save_job(job)
                self.tasks.pop(job_id, None); self.stops.pop(job_id, None)
        return self.get(job_id)

    def _adoption_matches(self, job):
        """Recover only a completed exact metadata selection after a crash."""
        try:
            draft = self.imports.inspect(job["import_id"])
            selected = self.preparations.selected_descriptor(job["import_id"])
            plan = self._plan(job["plan_id"])
            return (draft["revision"] == job["revision"] + 1 and draft["source"]["revision"] == job["source_revision"]
                and selected is not None and selected["revision"] == draft["revision"]
                and selected["source_revision"] == job["source_revision"]
                and selected["candidate_id"] == job["adopting_candidate_id"]
                and selected["form_digest"] == plan["plan_digest"]
                and selected["descriptor_digest"] == job["adopting_descriptor_digest"])
        except (KeyError, ValueError):
            return False

    async def adopt(self, payload):
        try:
            value = AdoptStart.model_validate(payload)
        except ValidationError:
            raise PreparationError("agent_start_invalid") from None
        job = self._job(value.job_id)
        if (job["state"] == "adopted" and job["local_io_settled"] and job["proposal_digest"] == value.proposal_digest
                and self._adoption_matches(job)):
            return {"schema_version": 1, "job": self.get(job["job_id"]), "draft": self.imports.inspect(job["import_id"]),
                "descriptor_digest": job["adopting_descriptor_digest"], "requires_preparation_review": True}
        if (job["state"] != "succeeded" or not job["local_io_settled"]
                or job["proposal_digest"] != value.proposal_digest):
            raise PreparationError("agent_candidate_unavailable")
        plan = self._plan(job["plan_id"])
        request, _, _ = self._validate(plan)
        result = self.store.proposal(job["attempt_id"], request, lambda _: self._validate(plan))
        if job["job_id"] in self.tasks:
            raise PreparationError("agent_job_busy")
        if len(self.tasks) >= 4:
            raise PreparationError("agent_pending_limit")
        stop = threading.Event(); self.stops[job["job_id"]] = stop
        job.update(state="adopting", local_io_settled=False); self._save_job(job)
        task = asyncio.create_task(self._adopt(job, plan, result, stop))
        self.tasks[job["job_id"]] = task
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            stop.set()
            await self.cancel(job["job_id"])
            raise

    async def _adopt(self, job, plan, result, stop):
        worker, committed = None, None
        def authority():
            if stop.is_set() or self._job(job["job_id"])["state"] == "cancel_requested":
                raise PreparationError("agent_cancel_requested")
            self._validate(plan)
            return True
        def prepare():
            if self.execution_admission is not None:
                self.execution_admission.check_current(full=True)
            def check():
                if stop.is_set():
                    raise PreparationError("agent_cancel_requested")
            check()
            descriptor, observed = self._candidate_descriptor(job, plan, result, check)
            candidate_id, resolution_digest = self._candidate_binding(descriptor, observed)
            checked = self.preparations.prepare_descriptor_selection(job["import_id"], job["revision"],
                descriptor.model_dump(mode="json", exclude_none=True), candidate_id=candidate_id,
                form_digest=plan["plan_digest"], resolution_digest=resolution_digest)
            if self.execution_admission is not None:
                self.execution_admission.check_current(full=True)
            if stop.is_set():
                raise PreparationError("agent_cancel_requested")
            return checked, descriptor.digest, candidate_id
        try:
            worker = asyncio.create_task(asyncio.to_thread(prepare))
            checked, descriptor_digest, candidate_id = await asyncio.shield(worker)
            authority()
            job.update(adopting_descriptor_digest=descriptor_digest, adopting_candidate_id=candidate_id); self._save_job(job)
            committed = self.preparations.commit_descriptor_selection(checked, check_current=authority)
            draft = json.loads(committed.draft)
            # Persist the commit winner before the first cleanup await. Recovery
            # also recognizes this exact metadata selection if this save fails.
            job.update(state="adopted", adopted_revision=draft["revision"]); self._save_job(job)
            worker = asyncio.create_task(asyncio.to_thread(self.preparations.cleanup_descriptor_selection, committed))
            await asyncio.shield(worker)
            return {"schema_version": 1, "job": {**self.get(job["job_id"]), "local_io_settled": True}, "draft": draft,
                "descriptor_digest": descriptor_digest, "requires_preparation_review": True}
        except asyncio.CancelledError:
            stop.set()
            if committed is None:
                job.update(state="canceled", error_code=None)
            raise
        except Exception:
            if committed is None:
                job.update(state="succeeded", error_code="agent_candidate_rejected")
            raise PreparationError("agent_candidate_rejected") from None
        finally:
            if worker is not None:
                try:
                    await _join(worker)
                except BaseException:
                    pass
            if committed is None and job["state"] == "canceled":
                self._cancel_generated_candidate(job)
            job["local_io_settled"] = True; self._save_job(job)
            self.tasks.pop(job["job_id"], None); self.stops.pop(job["job_id"], None)

    async def dispatch(self, method, path, payload=None, *, owner_authorized=False):
        """Called only after normal socket/Host/Origin authentication by the host.

        Old preparation grants must never set owner_authorized. No client field
        can opt in to either execution gate.
        """
        if owner_authorized is not True:
            raise PreparationError("agent_owner_required")
        payload = payload or {}
        if method == "POST" and path == "/v1/agent-preparations/plans":
            if set(payload) == {"import_id", "revision"}:
                return self.options(payload["import_id"], payload["revision"])
            return await self.plan(payload)
        if method == "POST" and path == "/v1/agent-preparations/start":
            if payload.get("action") == "adopt-candidate":
                return await self.adopt(payload)
            return await self.start(payload)
        match = re.fullmatch(r"/v1/agent-preparations/jobs/([0-9a-f-]{36})(/cancel)?", path)
        if match and not payload:
            if method == "GET" and match[2] is None:
                return self.get(match[1])
            if method == "POST" and match[2] is not None:
                return await self.cancel(match[1])
        raise PreparationError("agent_route_invalid")

    async def close(self):
        self.closing = True
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close())
        return await _join(self._close_task)

    async def _close(self):
        for job_id in list(self.tasks):
            await self.cancel(job_id)
        for worker in list(self.static_workers):
            try:
                await _join(worker)
            except BaseException:
                pass
        self.store.close()


def agent_preparation_routes(local_policy, agent_host):
    """Add closed owner routes inside the existing authenticated LocalPolicy.

    The trusted Supervisor registers this once. No route can create a provider
    configuration or change either gate. Existing scoped grants fail here even
    when their old prepare scope permits unrelated draft metadata operations.
    """
    from starlette.responses import JSONResponse
    from starlette.routing import Route
    from .policy import LocalPolicy, load_json
    if not isinstance(local_policy, LocalPolicy):
        raise PreparationError("agent_owner_policy_required")
    async def dispatch(request):
        principal = request.scope.get("mcpme.principal", {})
        if principal.get("owner") is not True or principal.get("grant_id") is not None:
            return JSONResponse({"api_version": 1, "error": {"code": "grant_scope"}}, status_code=403)
        try:
            payload = None if request.method == "GET" else load_json(await request.body())
            if payload is not None and not isinstance(payload, dict):
                raise PreparationError("agent_request_invalid")
            result = await agent_host.dispatch(request.method, request.url.path, payload, owner_authorized=True)
            return JSONResponse(result | {"api_version": 1})
        except Exception:
            # Includes imported source/recipe errors: no raw strings or payloads
            # from a provider or extension enter response diagnostics.
            return JSONResponse({"api_version": 1, "error": {"code": "agent_request_rejected"}}, status_code=400)
    local_policy.app.routes.extend([
        Route("/v1/agent-preparations/plans", dispatch, methods=["POST"]),
        Route("/v1/agent-preparations/start", dispatch, methods=["POST"]),
        Route("/v1/agent-preparations/jobs/{job_id}", dispatch, methods=["GET"]),
        Route("/v1/agent-preparations/jobs/{job_id}/cancel", dispatch, methods=["POST"]),
    ])
    return local_policy
