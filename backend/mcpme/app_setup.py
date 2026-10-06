"""One owner-reviewed, bounded generate/build/test/repair app setup workflow.

The workflow owns existing proposal, dependency and confined preparation jobs.
It grants no shell or ambient filesystem access to the LLM, never installs an
app, and never changes accepted tests in response to a failing build.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from typing import Annotated

from pydantic import Field, ValidationError

from .app_setup_contracts import RepairContextV1, diagnostic, safe_code
from .contracts import StrictModel
from .preparation_agent_contracts import AgentProposalV1, Identity, Digest
from .preparation_agent_host import _join
from .preparation_contracts import PreparationError, canonical_bytes, contract_digest

TERMINAL = {"ready", "failed", "canceled", "interrupted"}
PHASES = {
    "queued": "Starting app setup…", "analyzing": "Writing the app's tool adapter…",
    "correcting": "Completing the generated setup…", "resolving": "Installing the app's dependencies…",
    "preparing": "Building the app and testing its tools…", "repairing": "Fixing the adapter using the test failure…",
    "ready": "The app passed its tests and is ready to add.", "failed": "App setup needs attention.",
    "canceled": "App setup canceled.", "interrupted": "App setup was interrupted.",
    "cancel_requested": "Stopping app setup…",
}
LIMITS = {"max_provider_calls": 8, "max_repair_attempts": 2, "max_preparation_attempts": 2,
    "max_output_tokens": 8192, "max_transfer_bytes": 2 * 1024**2, "wall_seconds": 1200,
    "max_reserved_output_tokens": 8 * 8192, "app_memory_mb": 2048,
    "preparation_memory_mb": 2048, "preparation_disk_bytes": 4 * 1024**3,
    "preparation_wall_seconds": 600, "resolution_wall_seconds": 300,
    "resolution_disk_bytes": 2 * 1024**3,
    "network": ["files.pythonhosted.org", "pypi.org"]}
REPAIRABLE = {"preparation_fixture_failed", "preparation_tool_schema_mismatch", "preparation_probe_failed",
    "preparation_command_failed", "preparation_probe_timeout"}


class SetupSelection(StrictModel):
    import_id: Identity
    revision: Annotated[int, Field(ge=1)]
    provider_id: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")]
    model: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")]


class SetupStart(StrictModel):
    plan_id: Identity
    plan_digest: Digest
    idempotency_key: Identity


class AppSetupHost:
    def __init__(self, host):
        self.host, self.agent = host, host.agent_preparations
        self.db = self.agent.db
        self.tasks, self.children = {}, {}
        self.closing = False
        self.closed = False
        self.db.execute("CREATE TABLE IF NOT EXISTS app_setup_plans (id TEXT PRIMARY KEY, data TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS app_setup_jobs (id TEXT PRIMARY KEY, idem TEXT UNIQUE NOT NULL, data TEXT NOT NULL)")
        for row in self.db.execute("SELECT data FROM app_setup_jobs").fetchall():
            job = json.loads(row[0])
            if job["state"] not in TERMINAL:
                job.update(state="interrupted", local_io_settled=self._settled(job), error=diagnostic("app_setup_restarted"))
                self._save(job)
        self.agent.app_setups = self

    def _plan(self, identity):
        row = self.db.execute("SELECT data FROM app_setup_plans WHERE id=?", (identity,)).fetchone()
        if row is None:
            raise PreparationError("app_setup_changed")
        return json.loads(row[0])

    def _job(self, identity):
        row = self.db.execute("SELECT data FROM app_setup_jobs WHERE id=?", (identity,)).fetchone()
        if row is None:
            raise PreparationError("app_setup_changed")
        return json.loads(row[0])

    def _save(self, job):
        self.db.execute("UPDATE app_setup_jobs SET data=? WHERE id=?", (canonical_bytes(job).decode(), job["job_id"]))

    def _check(self, plan, job=None):
        if self.closing or time.time() >= plan["expires_at"]:
            raise PreparationError("app_setup_deadline")
        if job is not None and self._job(job["job_id"])["state"] == "cancel_requested":
            raise asyncio.CancelledError
        revision = job["revision"] if job else plan["revision"]
        draft = self.agent._draft(plan["import_id"], revision)
        if (draft["source"]["revision"] != plan["source_revision"]
                or contract_digest("agent-host-source-v1", draft["source"]) != plan["source_identity_digest"]
                or self.agent._snapshot_metadata(draft) != plan["snapshot_metadata_digest"]):
            raise PreparationError("app_setup_changed")
        provider = self.agent._provider(plan["provider_id"], plan["model"])
        if provider.digest != plan["provider_digest"]:
            raise PreparationError("agent_provider_changed")
        if not self.agent._available(provider, plan["model"]):
            raise PreparationError("agent_execution_disabled")
        ref = plan["recipe"]
        self.agent.recipes.assert_admissible(ref["id"], ref["version"], ref["digest"])
        return draft

    async def plan(self, payload):
        if self.closing:
            raise PreparationError("app_setup_changed")
        value = SetupSelection.model_validate(payload)
        options = self.agent.options(value.import_id, value.revision)
        preferred = [choice for choice in options["recipe_choices"] if choice["recommended"]
            and choice["recipe"]["id"] in {"mcpme.agent-python-adapter", "mcpme.agent-unlocked-python-adapter"}]
        if len(preferred) != 1:
            raise PreparationError("app_setup_python_required")
        paths = sorted(item["path"] for item in options["files"])
        metadata = sorted(item["path"] for item in options["metadata_files"] if item["path"] not in paths)
        if (not paths or options["files_truncated"] or options["metadata_files_truncated"]
                or len(paths) > 32 or len(metadata) > 32
                or sum(item["size"] for item in options["files"]) > 128 * 1024):
            raise PreparationError("app_setup_folder_limit")
        selected = {**value.model_dump(), "recipe": preferred[0]["recipe"], "selected_paths": paths,
            "selected_metadata_paths": metadata,
            "limits": {"max_output_tokens": LIMITS["max_output_tokens"], "max_transfer_bytes": LIMITS["max_transfer_bytes"],
                "wall_seconds": 360}}
        draft = self.agent._draft(value.import_id, value.revision)
        if not draft["intent"]:
            provider = self.agent._provider(value.provider_id, value.model)
            return {"schema_version": 1, "plan_id": str(uuid.uuid4()), "plan_digest": "0" * 64,
                **value.model_dump(), "source_revision": draft["source"]["revision"], "destination": provider.destination,
                "provider_generation": provider.generation, "selected_file_count": len(paths),
                "selected_metadata_count": len(metadata), "selected_bytes": sum(item["size"] for item in options["files"]),
                "limits": LIMITS, "expires_at": int(time.time()) + 300,
                "execution_available": False, "blockers": ["agent_intent_required"],
                "monetary_ceiling_enforced": False, "remote_usage_after_cancel": "unknown"}
        reviewed = await self.agent.plan(selected)
        inner = self.agent._plan(reviewed["plan_id"])
        plan = {"schema_version": 1, "plan_id": str(uuid.uuid4()), "agent_plan_id": reviewed["plan_id"],
            "agent_selection": selected, "recipe": preferred[0]["recipe"], **value.model_dump(),
            "source_revision": reviewed["source_revision"], "source_identity_digest": inner["source_identity_digest"],
            "snapshot_metadata_digest": inner["snapshot_metadata_digest"], "provider_digest": inner["provider_digest"],
            "provider_generation": reviewed["provider_generation"], "destination": reviewed["destination"],
            "selected_file_count": len(paths), "selected_metadata_count": len(metadata),
            "selected_bytes": sum(item["bytes"] for item in reviewed["selected_files"]),
            "limits": LIMITS, "expires_at": int(time.time()) + LIMITS["wall_seconds"], "job_id": None}
        plan["plan_digest"] = contract_digest("app-setup-plan-v1", {key: item for key, item in plan.items() if key != "job_id"})
        if self.db.execute("SELECT count(*) FROM app_setup_plans").fetchone()[0] >= 1024:
            raise PreparationError("app_setup_budget_exhausted")
        self.db.execute("INSERT INTO app_setup_plans VALUES (?,?)", (plan["plan_id"], canonical_bytes(plan).decode()))
        blockers = list(reviewed["blockers"])
        if not self.host.preparation_runner.enabled:
            blockers.append("preparation_runner_unavailable")
        if plan["recipe"]["id"] == "mcpme.agent-unlocked-python-adapter" and not self.host.resolution_runner.enabled:
            blockers.append("resolution_runner_unavailable")
        return {key: plan[key] for key in ("schema_version", "plan_id", "plan_digest", "import_id", "revision",
            "source_revision", "provider_id", "model", "provider_generation", "destination", "selected_file_count",
            "selected_metadata_count", "selected_bytes", "limits", "expires_at")} | {
            "execution_available": not blockers, "blockers": blockers,
            "monetary_ceiling_enforced": False, "remote_usage_after_cancel": "unknown"}

    def latest(self, import_id):
        draft = self.host.imports.inspect(import_id)
        for row in self.db.execute("SELECT data FROM app_setup_jobs ORDER BY rowid DESC"):
            job = json.loads(row[0])
            if job["import_id"] == import_id and job["source_revision"] == draft["source"]["revision"]:
                return self.get(job["job_id"])
        return None

    def get(self, job_id):
        job = self._job(job_id)
        calls = []
        for identity in job["agent_jobs"]:
            calls.extend(self.agent.get(identity)["calls"])
        draft = None
        if job["state"] == "ready":
            current = self.host.imports.inspect(job["import_id"])
            if (current["revision"] == job["revision"] and current["source"]["revision"] == job["source_revision"]
                    and current.get("attachment") is not None
                    and contract_digest("app-setup-attachment-v1", current["attachment"]) == job.get("attachment_digest")):
                draft = current
            else:
                job.update(state="failed", error=diagnostic("app_setup_changed"))
        return {key: job[key] for key in ("schema_version", "job_id", "import_id", "revision", "source_revision", "state",
            "local_io_settled", "attempt", "agent_job_id", "preparation_job_id", "error")} | {
            "phase_message": PHASES[job["state"]], "calls_used": len(calls),
            "transfer_bytes_used": sum(call["transfer_bytes"] for call in calls),
            "draft": draft,
            "installed": False, "remote_usage_after_cancel": "unknown"}

    async def start(self, payload):
        value = SetupStart.model_validate(payload)
        plan = self._plan(value.plan_id)
        if value.plan_digest != plan["plan_digest"]:
            raise PreparationError("app_setup_changed")
        previous = self.db.execute("SELECT data FROM app_setup_jobs WHERE idem=?", (value.idempotency_key,)).fetchone()
        if previous:
            old = json.loads(previous[0])
            if old["plan_id"] != value.plan_id:
                raise PreparationError("app_setup_changed")
            return self.get(old["job_id"])
        self._check(plan)
        if plan["job_id"] is not None:
            raise PreparationError("app_setup_changed")
        if len(self.tasks) >= 4 or any(json.loads(row[0])["import_id"] == plan["import_id"]
                and (json.loads(row[0])["state"] not in TERMINAL or not json.loads(row[0])["local_io_settled"])
                for row in self.db.execute("SELECT data FROM app_setup_jobs")):
            raise PreparationError("app_setup_busy")
        if not self.host.preparation_runner.enabled:
            raise PreparationError("preparation_runner_unavailable")
        job = {"schema_version": 1, "job_id": str(uuid.uuid4()), "plan_id": plan["plan_id"],
            "import_id": plan["import_id"], "revision": plan["revision"], "source_revision": plan["source_revision"],
            "state": "queued", "local_io_settled": False, "attempt": 1, "error": None,
            "agent_job_id": None, "agent_jobs": [], "preparation_job_id": None, "preparation_jobs": [],
            "resolution_jobs": [], "accepted_test_digest": None, "attachment_digest": None}
        with self.agent.store.transaction():
            plan["job_id"] = job["job_id"]
            self.db.execute("UPDATE app_setup_plans SET data=? WHERE id=?", (canonical_bytes(plan).decode(), plan["plan_id"]))
            self.db.execute("INSERT INTO app_setup_jobs VALUES (?,?,?)", (job["job_id"], value.idempotency_key, canonical_bytes(job).decode()))
        self.tasks[job["job_id"]] = asyncio.create_task(self._run(job, plan))
        return self.get(job["job_id"])

    async def _owned(self, job, work):
        task = asyncio.create_task(work)
        self.children[job["job_id"]] = task
        try:
            return await asyncio.shield(task)
        finally:
            try:
                await _join(task)
            except BaseException:
                pass
            self.children.pop(job["job_id"], None)

    def _phase(self, job, state):
        if self._job(job["job_id"])["state"] == "cancel_requested":
            raise asyncio.CancelledError
        job["state"] = state
        self._save(job)

    async def _analyze(self, job, plan, reviewed):
        self._check(plan, job)
        async def start():
            result = await self.agent.start({"action": "analyze", "plan_id": reviewed["plan_id"],
                "plan_digest": reviewed["plan_digest"], "idempotency_key": str(uuid.uuid4())})
            job["agent_jobs"].append(result["job_id"])
            job["agent_job_id"] = result["job_id"]
            self._save(job)
            return result
        result = await self._owned(job, start())
        task = self.agent.tasks.get(result["job_id"])
        if task is not None:
            await asyncio.shield(task)
        result = self.agent.get(result["job_id"])
        if result["state"] != "succeeded":
            raise PreparationError(safe_code(result.get("error_code")))
        inner = self.agent._plan(reviewed["plan_id"])
        request = self.agent._request(inner, self._check(plan, job))
        proposal = self.agent.store.proposal(result["attempt_id"], request, lambda _: self.agent._validate(inner))
        return result, proposal

    async def _repair(self, job, plan, previous, proposal, roles, mode, code):
        self._check(plan, job)
        used = self.get(job["job_id"])
        if used["calls_used"] + len(roles) > plan["limits"]["max_provider_calls"]:
            raise PreparationError("app_setup_budget_exhausted")
        remaining = plan["limits"]["max_transfer_bytes"] - used["transfer_bytes_used"]
        if remaining <= 0:
            raise PreparationError("agent_transfer_limit")
        selection = {**plan["agent_selection"], "revision": job["revision"],
            "limits": {**plan["agent_selection"]["limits"], "max_transfer_bytes": remaining,
                "wall_seconds": max(1, min(360, plan["expires_at"] - int(time.time())))}}
        context = RepairContextV1(workflow_id=job["job_id"], prior_attempt_id=previous["attempt_id"],
            prior_proposal_digest=proposal.digest, mode=mode, roles=roles, diagnostic_code=safe_code(code))
        reviewed = await self._owned(job, self.agent.plan(selection, repair_context=context.model_dump()))
        return await self._analyze(job, plan, reviewed)

    async def _candidate(self, job, plan, analysis, proposal):
        """Freeze/validate through the registered family, retaining exact diagnostics."""
        current = self._check(plan, job)
        inner_job = self.agent._job(analysis["job_id"])
        inner_plan = self.agent._plan(inner_job["plan_id"])
        try:
            descriptor, _ = await self._owned(job, self.host.static_work(
                self.agent._candidate_descriptor, inner_job, inner_plan, proposal))
            candidate_id, _ = await self._owned(job, self.host.static_work(self.agent._candidate_binding, descriptor, current))
            return candidate_id
        except PreparationError as error:
            if error.code != "agent_dependency_resolution_required":
                raise
            inspection = await self._owned(job, self.host.static_work(self.host.preparations.inspect_source, current))
            matches = [item for item in inspection["candidates"] if item.get("_agent_job_id") == analysis["job_id"]]
            if len(matches) != 1:
                raise PreparationError("agent_dependency_resolution_required")
            return matches[0]["candidate_id"]
        except (ValidationError, TypeError, KeyError):
            raise PreparationError("agent_adapter_proposal_invalid") from None

    async def _build(self, job, plan, candidate_id):
        self._check(plan, job)
        async def context(resolution=None):
            return await self.host.static_work(self.host.preparation_forms.context, job["import_id"], job["revision"],
                candidate_id=candidate_id, recipe=plan["recipe"], execution_available=True,
                resolution_available=self.host.resolution_runner.enabled, resolution_digest=resolution)
        form_context = await self._owned(job, context())
        if form_context.form.resolution_required:
            self._phase(job, "resolving")
            resolution = await self._owned(job, self.host.resolution_runner.plan(job["import_id"], job["revision"],
                candidate_id, form_context.form.resolution_policy))
            self._check(plan, job)
            async def start_resolution():
                value = await self.host.resolution_runner.start(resolution["plan_id"], resolution["plan_digest"], str(uuid.uuid4()))
                job["resolution_jobs"].append(value["job_id"]); self._save(job)
                return value
            resolving = await self._owned(job, start_resolution())
            task = self.host.resolution_runner.tasks.get(resolving["job_id"])
            if task is not None:
                await asyncio.shield(task)
            resolved = self.host.resolutions.get(resolving["job_id"])
            if resolved["state"] != "succeeded" or not resolved["ownership_settled"]:
                raise PreparationError(safe_code(resolved.get("error_code"), "dependency_resolution_failed"))
            self._check(plan, job)
            form_context = await self._owned(job, context(resolved["result_digest"]))
        if form_context.form.blockers:
            raise PreparationError(safe_code(form_context.form.blockers[0]))
        # Only the host-selected resource field is defaulted. Tests and schemas
        # remain exactly the accepted generated contract throughout repair.
        answers = {field.id: min(plan["limits"]["app_memory_mb"], field.constraints.get("maximum", 4096))
            for field in form_context.form.fields if field.label == "Memory budget (MiB)"}
        form = form_context.form
        descriptor, _ = await self._owned(job, self.host.static_work(self.host.preparation_forms.resolve,
            job["import_id"], job["revision"], candidate_id=candidate_id, recipe=plan["recipe"],
            form_digest=form.form_digest, answers=answers, resolution_digest=form.resolution_digest,
            execution_available=True, resolution_available=self.host.resolution_runner.enabled))
        raw = descriptor.model_dump(mode="json", exclude_none=True)
        fixed = contract_digest("app-setup-accepted-test-v1", {"tools": raw["tools"], "validation": raw["validation"]})
        if job["accepted_test_digest"] is not None and fixed != job["accepted_test_digest"]:
            raise PreparationError("agent_adapter_fixture_mismatch")
        job["accepted_test_digest"] = fixed
        self._check(plan, job)
        # Selection increments the draft revision. Capture that transition in
        # the same owned operation even if the caller is canceled mid-selection.
        async def select():
            draft = await self.host.static_work(self.host.preparations.select_descriptor, job["import_id"], job["revision"], raw,
                candidate_id=candidate_id, form_digest=form.form_digest, resolution_digest=form.resolution_digest)
            job["revision"] = draft["revision"]; self._save(job)
            return draft
        await self._owned(job, select())
        self._phase(job, "preparing")
        self._check(plan, job)
        policy = {"schema_version": 1, "operations": ["build", "probe"], "network": ["files.pythonhosted.org"],
            "wall_seconds": min(600, max(1, plan["expires_at"] - int(time.time()))),
            "disk_bytes": plan["limits"]["preparation_disk_bytes"], "memory_mb": plan["limits"]["preparation_memory_mb"]}
        prepared = await self._owned(job, self.host.preparation_runner.plan(job["import_id"], job["revision"], raw, policy))
        self._check(plan, job)
        async def start_build():
            value = await self.host.preparation_runner.start(prepared["plan_id"], prepared["plan_digest"], str(uuid.uuid4()))
            job["preparation_jobs"].append(value["job_id"]); job["preparation_job_id"] = value["job_id"]; self._save(job)
            return value
        building = await self._owned(job, start_build())
        task = self.host.preparation_runner.tasks.get(building["job_id"])
        if task is not None:
            await asyncio.shield(task)
        built = self.host.preparations.get(building["job_id"])
        if built["state"] != "succeeded" or not built["ownership_settled"]:
            raise PreparationError(safe_code(built.get("error_code")))
        draft = self.host.imports.inspect(job["import_id"])
        if (draft["revision"] != job["revision"] or draft["source"]["revision"] != plan["source_revision"]
                or draft.get("attachment") is None):
            raise PreparationError("app_setup_changed")
        job["attachment_digest"] = contract_digest("app-setup-attachment-v1", draft["attachment"])

    async def _run(self, job, plan):
        try:
            async with asyncio.timeout(max(1, plan["expires_at"] - time.time())):
                self._phase(job, "analyzing")
                first = self.agent._plan(plan["agent_plan_id"])
                analysis, proposal = await self._analyze(job, plan, first)
                try:
                    candidate = await self._candidate(job, plan, analysis, proposal)
                except PreparationError as error:
                    code = safe_code(error)
                    roles = [item.role for item in proposal.tasks if item.unresolved]
                    if not roles:
                        roles = ["adapter"] if code in {"agent_adapter_static_eligibility_required", "agent_adapter_interface_mismatch"} else ["interface", "dependencies", "fixtures", "adapter"]
                    if "interface" in roles and "fixtures" not in roles:
                        roles.append("fixtures")
                    if "adapter" not in roles:
                        roles.append("adapter")
                    roles = [role for role in ("interface", "dependencies", "fixtures", "adapter") if role in roles]
                    self._phase(job, "correcting")
                    analysis, proposal = await self._repair(job, plan, analysis, proposal, roles, "complete-proposal", code)
                    candidate = await self._candidate(job, plan, analysis, proposal)
                try:
                    await self._build(job, plan, candidate)
                except PreparationError as error:
                    code = safe_code(error)
                    if code not in REPAIRABLE or job["accepted_test_digest"] is None:
                        raise
                    self._phase(job, "repairing")
                    analysis, proposal = await self._repair(job, plan, analysis, proposal, ["adapter"], "repair-adapter", code)
                    job["attempt"] = 2; self._save(job)
                    candidate = await self._candidate(job, plan, analysis, proposal)
                    await self._build(job, plan, candidate)
                job["state"] = "ready"
        except asyncio.CancelledError:
            job.update(state="canceled", error=None)
        except TimeoutError:
            job.update(state="failed", error=diagnostic("app_setup_deadline"))
        except Exception as error:
            job.update(state="failed", error={**diagnostic(error), "stage": PHASES.get(job["state"], "App setup")})
        finally:
            # Keep the workflow unsettled until every owned network/process
            # child has completed its ordinary cancellation and ownership join.
            for identity in job["agent_jobs"]:
                if identity in self.agent.tasks:
                    await self._settle(self.agent.cancel(identity))
            for runner, identities in ((self.host.resolution_runner, job["resolution_jobs"]),
                    (self.host.preparation_runner, job["preparation_jobs"])):
                for identity in identities:
                    task = runner.tasks.get(identity)
                    if task is not None and not task.done():
                        await self._settle(runner.cancel(identity))
            job["local_io_settled"] = self._settled(job)
            if not job["local_io_settled"]:
                job.update(state="failed", error=diagnostic("app_setup_ownership_unsettled"))
            self._save(job)
            self.tasks.pop(job["job_id"], None)

    def _settled(self, job):
        try:
            if any(not self.agent.get(identity)["local_io_settled"] for identity in job["agent_jobs"]):
                return False
            for store, identities in ((getattr(self.host, "resolutions", None), job["resolution_jobs"]),
                    (self.host.preparations, job["preparation_jobs"])):
                for identity in identities:
                    value = store.get(identity)
                    if not value["ownership_settled"] or value["reservation_retained"]:
                        return False
            return True
        except Exception:
            return False

    @staticmethod
    async def _settle(work):
        task = asyncio.create_task(work)
        try:
            return await _join(task)
        except BaseException:
            return None

    async def cancel(self, identity):
        job = self._job(identity)
        task = self.tasks.get(identity)
        if job["state"] not in TERMINAL:
            job["state"] = "cancel_requested"; self._save(job)
            if task is not None:
                task.cancel()
        if task is not None:
            try:
                await _join(task)
            except BaseException:
                pass
        job = self._job(identity)
        if job["state"] == "cancel_requested":
            job.update(state="canceled", local_io_settled=True, error=None); self._save(job)
            self.tasks.pop(identity, None)
        if not job["local_io_settled"] and task is None:
            for runner, identities in ((self.host.resolution_runner, job["resolution_jobs"]),
                    (self.host.preparation_runner, job["preparation_jobs"])):
                for child_id in identities:
                    await self._settle(runner.cancel(child_id))
            job["local_io_settled"] = self._settled(job); self._save(job)
        return self.get(identity)

    async def close(self):
        if self.closed:
            return
        self.closing = True
        for identity in list(self.tasks):
            await self.cancel(identity)
        for row in self.db.execute("SELECT data FROM app_setup_jobs").fetchall():
            job = json.loads(row[0])
            if not job["local_io_settled"]:
                value = await self.cancel(job["job_id"])
                if not value["local_io_settled"]:
                    raise PreparationError("app_setup_ownership_unsettled")
        self.closed = True


def app_setup_routes(policy, setups):
    from starlette.responses import JSONResponse
    from starlette.routing import Route
    from .policy import load_json

    async def dispatch(request):
        principal = request.scope.get("mcpme.principal", {})
        if principal.get("owner") is not True or principal.get("grant_id") is not None:
            return JSONResponse({"api_version": 1, "error": {"code": "grant_scope"}}, status_code=403)
        try:
            payload = {} if request.method == "GET" else load_json(await request.body())
            if not isinstance(payload, dict):
                raise PreparationError("app_setup_changed")
            path = request.url.path
            if request.method == "POST" and path == "/v1/app-setups/plans":
                value = await setups.plan(payload)
            elif request.method == "POST" and path == "/v1/app-setups/start":
                value = await setups.start(payload)
            else:
                match = re.fullmatch(r"/v1/app-setups/jobs/([0-9a-f-]{36})(/cancel)?", path)
                if match is None or payload:
                    raise PreparationError("app_setup_changed")
                value = await setups.cancel(match[1]) if match[2] else setups.get(match[1])
            return JSONResponse({"api_version": 1, **value})
        except (Exception, ValidationError) as error:
            return JSONResponse({"api_version": 1, "error": diagnostic(error)}, status_code=400)

    policy.app.routes.extend([
        Route("/v1/app-setups/plans", dispatch, methods=["POST"]),
        Route("/v1/app-setups/start", dispatch, methods=["POST"]),
        Route("/v1/app-setups/jobs/{job_id}", dispatch, methods=["GET"]),
        Route("/v1/app-setups/jobs/{job_id}/cancel", dispatch, methods=["POST"]),
    ])
    return policy
